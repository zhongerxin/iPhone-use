---
name: iphone-use-setup
description: PUA（Phone Use Agent）を設定し、ユーザー自身のiPhoneで実行サービスを署名・インストール・起動する。新しいチャットでは先にsetupでサービスを確認・起動してからREADYを取得し、既存の設定とビルドを再利用する。USB・Xcodeを診断し、画面サイドバーを表示する。初回導入、コールドスタート、切断復旧、署名期限切れに使い、日本語で案内する。
---

# iPhoneをREADYにする

プラグインのMCPツールで検出、ビルド、起動、接続を行う。各作業で一時的なPythonクライアントを生成しない。使えるPUAがあれば再利用し、毎回ビルド・再導入しない。

## 現在の状態を調べる

このチャットでREADYを取得していなければ、先に`pua_setup(action="status")`を呼び、下の手順で正常なサービスか活動中のジョブを再利用する。必要なサービスがなければstartを一度だけ呼ぶ。サービスが使える状態になってから`pua_ready(recover=true, screenshot=false)`を呼び、操作前に確認する。READYの失敗を待ってからsetupを始めない。以前のチャットのREADYやプラグイン導入を証拠にしない。このチャットですでにREADYで接続が正常なら、doctor／discover／build／startを繰り返さない。初回設定や具体的な不足がある場合は`pua_doctor`と`pua_setup(action="discover")`でXcode、接続端末、署名、ポート、プロセスを調べる。複数端末はユーザーの指定に従い、条件に合う端末が1台だけならそれを使う。UDID、Team ID、ログ、署名設定はユーザーのMacに保存し、ソースやGitに入れない。

通常は`recover=true`または省略する。falseは明示的な再起動禁止・読み取り専用診断に限る。画像が不要なら`screenshot=false`。READYのproofはstatus、利用可能なsession、実際の前面アプリ、viewport、ロック解除、現在のobservationを含む。その観察を次に使い、直後のobserveや移動テストを重ねない。ミラーリングが動作中でツリーが空なら`mirroring_conflict`となり、ミラーリングを終了して再確認する。Runnerアイコン、BUILD SUCCEEDED、開いたポートだけではREADYではない。

READYは対応ホストで画面サイドバーも開く。閉じた画面の再表示は`pua_screen()`を使う。プレビューは独立したUSB MJPEG（既定の端末ポート9100）で、XMLやスクリーンショットのポーリングではない。プレビューの不具合だけで操作接続を繰り返し再起動しない。通常操作は次の判断に必要な観察で進捗を確認し、重要な最終結果を明示検証する。

`phone_locked`は`device_locked`としてプレビューを自動停止する。明示pauseで上書きしない。本人からロック解除の完了回答を受けてREADYを再確認し、成功時に同じロック停止だけ解除する。アプリ認証やunknown停止は明示的なresumeが必要。READYのpreviewで理由を調べ、空白を接続障害とみなさない。

## サービス未起動なら初期化を続ける

`recover=true`は、所有者を確認したサービスの継続する`local.pid.0`／XCTest障害を復旧できるが、初回設定やコールドスタートを自動完了しない。`pua_unreachable`、接続拒否、`not_ready`なら以下を続ける。

1. `pua_setup(action="status")`で`configured`、`service`、`jobs`を読む。configured=trueなら既存の端末・署名・ポートを保持する。falseの場合だけ不足を初回手順で補う。
2. 現在の設定／endpointに合うstart／recoverがqueuedまたはrunningなら同じidを記録して`pua_setup(action="status", job_id=..., wait_seconds=20)`で待つ。statusは`jobs`配列を返すためidで選ぶ。無関係な古いジョブを待たない。対応するfetch／buildが実行中ならそれも再利用する。
3. `service.ready=true`でもsession／前面／viewportをREADYで確認する。startがrunningでもサービスが使えればREADYへ進む。recoverは`recovery_phase="serving"`になってからREADY。長期動作するRunnerのsucceededを待たない。
4. 設定済み、サービス停止、対応する活動ジョブなしなら`pua_setup(action="start")`を一度呼んで有効なビルドを使う。startは最大20秒待ち、サービスが使える状態かジョブの終了を確認したら早く返る。固定のsleepではない。`service.ready=true`ならstatusを重ねずREADYへ進む。未完了なら、新しい`job_id`またはalready_running=trueの`job.id`を記録し、`pua_setup(action="status", job_id=..., wait_seconds=20)`で同じジョブを待つ。タイムアウト後もジョブは続くためstartを重ねない。ソース不足を明示された場合だけfetch、有効なビルド不足・署名期限切れ・バイナリー非互換ならbuildしてからstart。失敗は正確なログとnext_stepsから処理し、startのループを作らない。
5. READYになったらその観察から元の作業を続ける。USB、Xcode、署名、権限の実際の障害を処理する。本人の信頼・ログイン・解除が必要なら先頭選択肢「完了したので続けてください」の質問ツールを使う。読み取り専用や再起動禁止を初期化で迂回しない。

既存ウィジェットを再利用する。ユーザーが先に画面表示を求めた場合は、未表示なら`pua_screen()`を呼び、初期化と許可済み作業を続ける。明示されていない承認待ちを追加しない。未起動の空白は全作業の失敗ではない。

## 初回導入

1. doctorの不足に応じて、端末のiOSに合う完全版Xcodeと初回起動設定を準備する。XcodeのAccountsに本人のApple Accountでログインする。パスワード、認証コード、ロック解除は本人がシステム画面で行う。
2. USBで接続し、iPhoneで「このコンピュータを信頼」を確認する。XcodeのDevices and Simulatorsまたはdiscoverで認識を確認する。Finderに表示されるだけでは開発用ペアリングの完了を証明しない。
3. 実際の表示に従い「設定 → プライバシーとセキュリティ → デベロッパモード」を有効にし、再起動後の確認を行う。項目がなければXcodeのペアリング・開発準備後に再確認する。必要なら「設定 → デベロッパ → UIオートメーションを有効にする」を確認する。OSの実際の表記に従う。
4. `pua_setup(action="fetch")`で固定バージョンを取得し、configureで本人の`udid`、`team_id`、署名可能な`bundle_id`を設定する。任意の`source_dir`、`local_port`、`device_port`を指定でき、既定はMac側18100、端末側8100。実行データは`~/.local/share/iphone-use`。作者のIDや署名を写さない。自動署名失敗はログに従ってRunner targetのSigning & Capabilitiesで本人のTeamを選び、必要な場合だけbundle IDやprovisioningを調整する。
5. build、startでビルド・導入・Runnerテスト・USB転送を起動する。fetch／build／startは背景ジョブなのでidを記録し、statusのjobs配列から選ぶ。fetch／buildは終状態、startは`service.ready=true`でREADYへ進み、succeededを待たず重複起動しない。現在の状態、ログ、`retry_after_seconds`を使って調べる。固定の長い間隔や固定回数で盲目的に待たない。USBを接続したまま端末をUIテストに使える状態に保つ。
6. 開発者が信頼されていない場合は端末の「設定 → 一般 → VPNとデバイス管理」など実際の開発者項目と提示に従う。企業用アプリの手順をすべての開発署名に当てはめない。完了後statusとREADYを確認する。

本人のログイン、パスワード、ロック解除、端末確認、Xcode導入が必要なら、正確な画面、理由、完了後に続ける内容を説明し、ホストの質問ツール（Defaultでは`functions.request_user_input_async`を優先）を必ず使う。先頭を「完了したので続けてください」、次を「今は完了できません」とする。非同期の戻り値、初期選択、経過時間は完了回答ではない。実際の回答まで依存する設定を進めない。独立した作業は続けられる。質問機能がない場合だけ文章で待つ。許可済みのローカル導入に一律の追加確認を挟まない。既存アプリの削除、共有証明書の失効、有料アカウントへの変更で回避しない。

## READYの確認と復旧

READYの端末、session、viewport、observationをそのまま使う。通常の操作は`observe="none", verify=false`、次に必要な未知ページはその操作でtree／screenshot／bothを返す。重要な最終結果をexpect／verifyまたは最終読み取りで確認する。

アプリ認証は[引き継ぎガイド](../iphone-use/references/authentication.ja.md)に従ってpause、質問、実際の完了回答、resume、新しい観察の順で進める。アプリ認証だけでbuild／start／recoverを繰り返さない。実際のロックや切断の場合にREADYを復旧する。ユーザー引き継ぎ後は古い座標を使わず現在のアプリを読む。

切断、再起動、テスト停止、USB転送終了では上のstatus手順で既存ジョブとビルドを再利用する。具体的な不足があるときだけdoctorを使い、足りない層を修復してREADYを確認する。

`XCTDaemonErrorDomain Code=41`、`local.pid.0`、`pua_foreground_unavailable`は接続障害。`pua_ready(screenshot=false)`はsessionの再作成と再読を1回行い、持続時だけ設定、endpoint、worker、待受ポートの所有者を確認したプラグインのサービスを背景復旧する。操作は再実行しない。recoveringは同じrecovery.job_idを追い、servingでREADY。recovery_requiredは復旧禁止のため、指示が許すときだけtrueで再呼び出し。`pua_recovery_required`の冷却・所有者・ビルドの理由に従い、ポート番号だけでプロセスを停止しない。[接続の復旧](references/recovery.ja.md)を参照する。

署名、容量、接続、開発モード、バージョンの問題は実際のログと[トラブルシューティング](references/troubleshooting.ja.md)で処理する。署名期限切れ、非互換、無効なビルドの場合だけ再ビルドする。READYまたはNEEDS_USER_ACTIONと、確認した層、不足、次の手順を示す。未確認の状態をREADYと呼ばない。

## 公式資料

上流ガイドは[Appleの開発者アカウント説明](https://developer.apple.com/help/account/basics/about-your-developer-account)に基づき、Personal Teamは1台に最大3アプリ、provisioning profileは発行から7日で期限切れとなり再ビルド・再導入が必要と説明している。無料アカウントの個人テストを永続動作と案内しない。

[Appleのデベロッパモード](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device)と[Appiumの実機準備](https://appium.github.io/appium-xcuitest-driver/latest/getting-started/device-setup/)を参照し、実際のXcode／iOSの提示と診断に従う。脱獄は不要。
