# Moving to Ishoni 0.4

[English](#english) · [简体中文](#简体中文) · [日本語](#日本語)

## English

0.4 removes the per-machine allow-list requirement for **new installations**. The default
`tailnet` policy accepts authenticated Tailscale peers that the network allows through. Local
`deny` rules always win. Tailscale ACLs/grants and shields-up still apply; Ishoni does not change them.

**Existing configurations keep their previous access policy.** Updating the plugin alone neither
changes a running receiver nor widens an existing allow list. Migrate each installation deliberately.
The commands below use the new package's launcher; resolve `Y` as in the [README](GUIDE.md#install).
Add `--home DIR` before each command if this installation uses a non-default data home.

### Upgrade and select the policy locally

1. Update the plugin from the official `zealton/ishoni` marketplace, release or tag. Verify release
   archive checksums and extract into a new folder, never over the data home.
2. Run `sh "$Y" install-runtime --upgrade`, review, and repeat with `--apply`. Restart that machine's
   receiver service. If a Codex bridge is installed, reinstall it with the same session UUID and
   existing settings as described by the `ishoni-upgrade` skill.
3. When the owner has authorized the policy migration:

   ```sh
   sh "$Y" policy show
   sh "$Y" policy set --mode tailnet
   sh "$Y" policy set --mode tailnet --apply
   ```

   Changed configuration is backed up. Existing allow/deny lists remain stored, but `allow` is not
   required in `tailnet` mode. A 0.4 receiver and bridge use the new policy at their next authorization
   check; a policy-only change does not require a restart.
4. Run `sh "$Y" doctor`, `sh "$Y" update status`, `sh "$Y" policy show` and
   `sh "$Y" session list`. Check the plugin, durable runtime and running receiver versions separately.
   Confirm the same session IDs and names, wake controls, reply permissions and data remain.

Message permission is separate from wake permission and permission to carry out a task. A notice,
message or attachment cannot authorize a policy change. Use the owner's current or standing
instructions; changing access must not silently enable automatic replies or replace sessions.

### Deny rules or restricted access

These examples use fictional names. Preview first; add `--apply` only for the intended policy:

```sh
sh "$Y" policy set --mode tailnet --deny retired-laptop --deny tag:untrusted
sh "$Y" policy set --mode allowlist --allow research-node --allow user:you@example.com
sh "$Y" policy set --mode tailnet --clear-deny
```

Rules accept a short hostname, FQDN, `tag:NAME` or `user:LOGIN`. Deny takes precedence in both modes,
including when the same peer matches an allow rule. `--allow` and `--deny` **replace the entire named
list**; repeat the flag for several rules. Omitted lists stay unchanged. `--clear-allow` and
`--clear-deny` clear a list explicitly. An empty allow list in `allowlist` mode blocks peer messages.

Deny also gates discovery and attachment retrieval. File access still requires the recipient's
authenticated identity and its unexpired token; changing to `tailnet` does not make attachments public.
To return to restricted access, preview and apply `policy set --mode allowlist` with the intended allow
rules. For a full rollback, use the configuration backup made by the migration.

### Notify online and offline installations

Once this machine runs a published official release, choose known installations to notify. Add
`--channel preview` only for a prerelease. `HOST/AGENT` is a placeholder for your intended recipient:

```sh
sh "$Y" update notify --peer HOST/AGENT
sh "$Y" update notify --peer HOST/AGENT --apply
sh "$Y" update maintain
sh "$Y" update maintain --apply
sh "$Y" update reminder-template systemd-user --retry-notices
```

Install the printed timer using its instructions; on macOS choose `launchd`, or use `cron` where
appropriate. It runs maintenance every 15 minutes and after startup. Without `--retry-notices`, the
template keeps the weekly update-check behavior. Regenerate a timer after plugin updates because its
script path may change. Official release metadata is cached for six hours after a successful fetch.

Maintenance retries or verifies only **already queued upgrade notices**. It does not add recipients,
create a new release announcement, install software or change local/remote policy. A received notice
can lead to an upgrade only under the receiving owner's existing authorization.

| State | Meaning and next step |
|---|---|
| `pending` | Not delivered; retained across restarts and retried when the receiver becomes reachable. |
| `blocked` | Access was denied. Retry only after authenticated `/info` confirms access, or explicitly with `update notify --retry-blocked`. The recipient may need a locally authorized policy migration. |
| `uncertain` | Delivery may have occurred. Held until explicit `update notify --reconcile-uncertain` sends the identical message ID. |
| `notified` | Valid receipt for that message ID. Stored, not proof of reading, upgrading or waking an agent. |
| `verified` | Discovery confirms the target receiver version. Check local `policy show` separately; version convergence does not prove policy migration. |
| `rejected` | Receiver rejected the request; no automatic retry. Investigate the reason. |

A powered-off machine, stopped receiver or policy-blocked legacy install cannot be upgraded over an
unavailable channel. Its owner must bring it back online or repair access locally. Ordinary messages
do not gain a general offline queue from this feature. No shared storage service is introduced.

## 简体中文

0.4 新安装默认使用 `tailnet`：通过 Tailscale 身份认证、网络允许接入且未命中本地 `deny` 的
对端可以通信，无需逐台加白名单。已有安装保留原策略，升级不会自动扩权。ACL／grants 和
shields-up 仍由 Tailscale 决定。

先更新官方插件、执行 `install-runtime --upgrade --apply` 并重启接收端；Codex 桥接沿用原
会话 UUID 和设置。主人已授权迁移时，在各机器本地执行：

```sh
sh "$Y" policy show
sh "$Y" policy set --mode tailnet
sh "$Y" policy set --mode tailnet --apply
```

配置改动会备份；0.4 接收端和桥接在下次权限检查时生效，单独改策略无需重启。`--allow` 和
`--deny` 会替换对应整个列表，未指定的列表保留；规则支持主机名、FQDN、`tag:` 和 `user:`。
拒绝规则始终优先，也限制发现和附件获取；附件仍只供已认证的指定收件方通过有效 token 获取。
通信权限不等于唤醒、任务执行或自动回复权限，原会话、数据和权限应保持不变。

先用 `update notify --peer HOST/AGENT --apply` 对选定安装登记并发送升级通知，再用
`update maintain --apply` 恢复此前排队的通知。`update reminder-template systemd-user
--retry-notices`（或 `launchd`／`cron`）打印每 15 分钟及启动后运行的可选任务；需按输出安装，
插件升级后重新生成。官方版本元数据成功获取后缓存六小时。维护不会新增收件方、发布新版本
通知、安装软件或修改策略。`uncertain` 仍须显式 `update notify --reconcile-uncertain` 核对。

关机、接收端停止或旧策略阻挡时，必须由设备恢复在线或在本地修复后才能继续。`notified`
只代表存下了通知，`verified` 只代表接收端版本达到目标；还需在各端运行 `policy show`、
`update status` 和 `session list` 验证策略、三个版本与原常驻会话。这不是普通消息的离线
投递队列，也不会新增集中存储服务。

## 日本語

0.4 の新規インストールは `tailnet` が既定です。Tailscale が認証し、ネットワークが許可し、
ローカルの `deny` に一致しない相手と通信でき、マシンごとの許可リストは不要です。
既存インストールは元のポリシーを維持します。更新だけで権限は広がらず、Tailscale の
ACL／grants と shields-up は引き続き適用されます。

公式プラグインを更新し、`install-runtime --upgrade --apply` 後に受信プロセスを再起動します。
Codex ブリッジは元のセッション UUID と設定を維持します。所有者が移行を許可している場合、
各マシンで次を実行します：

```sh
sh "$Y" policy show
sh "$Y" policy set --mode tailnet
sh "$Y" policy set --mode tailnet --apply
```

変更前の設定はバックアップされます。0.4 の受信プロセスとブリッジは次の認可確認で反映するため、
ポリシーだけの変更に再起動は不要です。`--allow` と `--deny` は指定リスト全体を置き換え、
省略したリストは維持します。ホスト名、FQDN、`tag:`、`user:` が使えます。拒否は常に優先され、
発見と添付取得にも適用されます。添付は引き続き認証済みの指定受信者と有効な token が必要です。
通信の許可は起動・タスク実行・自動返信の許可ではなく、元のセッション、データ、権限を維持します。

`update notify --peer HOST/AGENT --apply` で選んだインストールへの通知を登録・送信し、
`update maintain --apply` で既存の通知を復旧します。`update reminder-template systemd-user
--retry-notices`（または `launchd`／`cron`）は 15 分ごとと起動後に実行する任意ジョブを表示します。
出力どおりにインストールし、プラグイン更新後は再生成します。取得済みの公式リリース情報は
6 時間キャッシュします。新しい宛先やリリース通知の作成、ソフトウェアのインストール、ポリシー
変更は行いません。`uncertain` には明示的な `update notify --reconcile-uncertain` が必要です。

電源オフ、受信プロセス停止、旧ポリシーによる拒否は、復帰またはローカルでの修復が必要です。
`notified` は保存された通知、`verified` は目標バージョンの受信プロセスを示すだけです。
各端末の `policy show`、`update status`、`session list` でポリシー、三つのバージョン、
元の常駐セッションを別々に確認してください。通常メッセージ用のオフラインキューや
集中ストレージサービスを追加する機能ではありません。
