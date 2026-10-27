# 设备租约约定（Device Lease，磁盘格式 1）

多个 AI 对话各自是一个进程，却可能操作同一部手机；同一部手机也可能有几条控制通道，比如 USB 上的 UI 自动化和硬件 KVM。手机的屏幕、前台 App 和输入焦点只有一份，所以同一时刻只能有一个进程动它。本约定让这些进程像用共享打印机一样轮流：空闲即用，占用即排队，连续操作期间保留控制权，用完归还。

实现只依赖文件和 `flock`，没有常驻服务、socket 或数据库。任何语言写的工具都可以按本约定加入同一个队列。参考实现是 `device_lease.py`，各通道原样复制使用，不在副本里改逻辑。

## 目录

每部物理设备一个目录：`~/.local/share/device-lease/<平台>-<设备标识>/`。环境变量 `DEVICE_LEASE_ROOT` 可以换掉根目录。

- **平台**：2 到 16 个小写字母或数字，例如 `ios`、`android`。被控制的设备本身也可以是一台设备，例如 KVM 用 `nanokvm`。
- **设备标识**：3 到 128 个字母、数字、点、下划线或连字符，必须对应硬件而不是连接。iOS 用 UDID；Android 用 `ro.serialno`，因为同一部手机经 USB 和 Wi-Fi 的 adb 会出现两个连接名；KVM 这类设备用证书指纹之类的硬件指纹。
- 同一部设备不论经哪个通道、哪个配置目录，都必须落到同一个目录；不同设备的目录互不影响。

| 文件 | 作用 |
|---|---|
| `device.lock` | 租约本体。持有者进程对它执行 `flock(LOCK_EX)` 并保持文件打开；进程退出时内核自动释放。文件内容无意义。 |
| `device-owner.json` | 当前或最近一位持有者的信息，原子写入（先写临时文件再 `rename`）。 |
| `device-queue/<ns>-<pid>.json` | 排队票据，每个等待进程一张。文件名前 20 位是补零的纳秒时间戳，字典序即先来后到。 |

所有文件权限 `0600`，目录 `0700`。

## 持有者记录 `device-owner.json`

```json
{"format": 1, "pid": 12345, "label": "山姆购物车", "transport": "wda",
 "generation": 7, "acquired_ts": 1761480000.1, "last_call_ts": 1761480042.8,
 "last_tool": "pua_tap", "released_at": 1761480103.0, "release_reason": "idle"}
```

- `pid`：持有者进程。进程已死且没有 `released_at`，说明它崩溃了。
- `label`：给人看的任务标签，由持有者自报；没有时显示「未命名任务」。
- `transport`：通道名，例如 `wda`、`kvm`、`adb`。缺省视为 `unknown`。
- `generation`：每次成功获取加一，用来判断自己上次持有之后是否有别人用过设备。
- `acquired_ts` / `last_call_ts`：Unix 时间戳（秒）。`last_call_ts` 由持有者每次调用后更新，排队方据此显示「多久没动」。
- `last_tool`：最近一次调用的名字，只用于显示。
- `released_at` / `release_reason`：正常归还时写入（`explicit` / `idle` / `closed`）。存在该字段即无人持有。

## 票据 `device-queue/<ns>-<pid>.json`

```json
{"format": 1, "pid": 23456, "label": "联通话费", "transport": "kvm",
 "enqueued_ts": 1761480050.0, "refreshed_ts": 1761480078.5}
```

票据有效的条件：`pid` 仍存活，且 `refreshed_ts` 距今不超过 TTL（默认 30 秒），且它不是当前持有者。等待进程在轮询时每次刷新 `refreshed_ts`；暂时退出轮询但仍想要设备的进程，由后台线程继续刷新最多 180 秒。无效票据任何参与者都可以删除。

## 规则

1. **获取**：写入自己的票据；循环：删除无效票据，按文件名排序，只有队头可以尝试 `flock(LOCK_EX | LOCK_NB)`；成功后删除自己的票据并写 `device-owner.json`，`generation` 为上一位加一；失败则刷新票据，等 0.25 秒再试。一次调用的等待预算默认 40 秒，要小于宿主的工具超时；超时向调用方返回「排队中」错误，附持有者标签、空闲秒数和自己的位置，并保留票据。
2. **非阻塞尝试**，例如用户在预览面板上的点击：队列非空时不插队，直接返回「被占用」，不留票据。
3. **持有**：每次调用后更新 `last_call_ts`。持有者活跃时不会被抢占；公平性来自空闲归还、显式归还和可见性。
4. **归还**：显式归还、空闲超过 60 秒或进程退出。正常归还先写 `released_at` 再解锁；进程崩溃时内核解锁，但没有 `released_at`。
5. **换手**：新持有者发现上一位 `pid` 不是自己时，先要求一次「新观察」，即读取当前屏幕，再允许会改变屏幕的动作；若上一位没有 `released_at`，说明它崩溃了，再多等 3 秒让可能在途的一条命令结束。不需要重启设备。
6. **感知**：任何参与者读取 `device-owner.json` 和 `device-queue/` 即可知道谁在用、在做什么、排了几个。

## 参考实现的可配置项

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `DEVICE_LEASE_ROOT` | `~/.local/share/device-lease` | 锁目录的根 |
| `DEVICE_LEASE_IDLE_RELEASE_SECONDS` | 60 | 无设备调用多久后自动归还 |
| `DEVICE_LEASE_WAIT_SECONDS` | 40 | 一次调用内最多排队等待多久 |
| `DEVICE_LEASE_HANDOVER_SETTLE_SECONDS` | 3 | 上一位崩溃时接手前的等待 |
| `DEVICE_LEASE_TICKET_TTL_SECONDS` | 30 | 票据不刷新多久算失效 |
| `DEVICE_LEASE_QUEUE_HOLD_SECONDS` | 180 | 返回「排队中」后替本进程继续保留位置多久 |

## 通道怎么接入

1. 把 `device_lease.py` 原样放进自己的代码，用 `device_directory(平台, 设备标识)` 算出设备目录，`DeviceLease.get(目录, transport=通道名)` 取得本进程唯一的租约对象。
2. 自己决定哪些调用要取租约、哪些会改变屏幕、哪些算一次新观察。只读的诊断、目录查询和画面预览一般不取租约；读屏可以取租约但不算改变屏幕。
3. 每次要取租约的调用包在 `with lease.hold(调用名, mutation=是否改变屏幕):` 里；读屏成功后调用 `lease.observed()`。`LeaseError` 表示什么都没执行，按自己的用户习惯把 `code` 和 `details` 翻译成提示。
4. 进程退出前调用 `lease.close()`，写入 `released_at` 后解锁。

同一个对话里从一条通道切到另一条，属于两个进程：先在原通道归还，否则对方要等空闲归还才能接手。
