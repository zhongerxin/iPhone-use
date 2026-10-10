# 起動と復旧

READYで`ready=true`が返らなければ、その状態に応じて続ける。作業全体の失敗とみなさず、すでに有効かもしれない業務操作を繰り返さない。読み取り専用、起動・再起動禁止の指示を守る。

## このチャットで最初に使う場合

このチャットでまだiPhone Useを使っておらず、有効なREADYがない場合は、先に`pua_setup(action="status")`を呼ぶ。サービスが正常ならREADYへ進む。活動中のジョブがあれば同じジョブを待つ。既存設定があり、サービスも活動ジョブもない場合だけstartを一度呼ぶ。READYの失敗を待ってからsetupを始めない。このチャットですでにREADYで接続が正常なら、そのまま作業を続ける。

startは最大20秒待ち、サービスが使える状態かジョブの終了を確認したら早く返る。固定のsleepではない。`service.ready=true`ならstatusを重ねずREADYへ進む。未完了ならjob_idを記録し、`pua_setup(action="status", job_id=..., wait_seconds=20)`で同じジョブを待つ。タイムアウトはジョブを取り消さず、startを重ねる許可にもならない。失敗時は具体的なログを読む。recoverは`recovery_phase="serving"`になってからREADYを確認する。setupのサービス確認は完全なREADYの代わりではなく、読み取り専用・起動禁止の指示も守る。

## サービスが未起動の場合

`recover=true`は所有者を確認したXCTest接続を復旧するが、停止中サービスをコールドスタートしない。`pua_unreachable`、接続拒否、`not_ready`などでは`initialization_required=true`とrecoveryのnext_tool／next_argumentsが次の初期化を示す。

1. `pua_setup(action="status")`でconfigured、service、jobsを調べる。
2. 現在の設定／endpointに合うstart／recoverがqueuedかrunningなら同じidを`pua_setup(action="status", job_id=..., wait_seconds=20)`で待つ。返されたjobs配列からidで選び、無関係な古いジョブを再利用しない。復旧のservingまたは起動のservice.ready=trueでREADYを再確認する。Runnerのsucceededを待たずstartも重ねない。対応するfetch／buildが実行中なら先にその結果を追う。
3. configured=true、未起動、活動ジョブなしならstartを一度呼ぶ。返されたjob_idまたはalready_running=trueのjob.idを記録する。ソースやビルドの不足・無効が明示されたときだけsetupスキルでfetch／buildを補う。サービスが使えるならREADYへ直接進む。
4. configured=falseなら`iphone-use-setup`を読み、不足に応じてdoctor／discover、fetch、configure、build／startを行う。本人の解除・ログイン・信頼は[認証の引き継ぎ](authentication.ja.md)で待つ。
5. READYのobservationから元の作業を続ける。実際の失敗や本人の操作待ちだけを正確な障害として報告する。

未起動中のプレビューの空白はREADYの証拠でも作業全体の失敗でもない。

## 復旧中の状態

- `ready=false, state="recovering"`：recovery.job_idとstatus引数で同じジョブを調べ、jobs配列から選ぶ。recovery_phase=servingでREADYを確認し、startを繰り返さない。
- `ready=false, state="recovery_required", reason="recovery_disabled"`：指示が復旧を許す場合だけnext_tool／next_argumentsでrecover=trueを使う。再起動禁止なら制限を守って報告する。

errorなし、MCP isError=falseでも操作可能とは限らない。復旧拒否、冷却、ロックは返された理由に従う。状態、ログ、retry_after_secondsに応じて調べ、固定の長いsleepや固定回数の空ポーリングをしない。

## プレビュー

独立したUSB MJPEGの最新キャッシュを画面が最大毎秒4回読む。画像・XMLのポーリングではなく、操作ロックも使わない。長い操作中も表示を更新する。最初の操作後の端の光は、認証停止、切断・ストリーム変更、終了・再読込で解除される。丸いカーソルはタップ位置・ドラッグ経路を示し、操作成功の証拠ではない。

非表示・終了時はポーリングを止め、プレビューのリースは5秒で切れる。認証停止は取得を止め画像を消し、本人の実際の完了回答後に再開する。更新後の古いチャットは旧プロセスや画面を保持し得るため、チャットを再接続して画面を開き直す。READYの繰り返しではキャッシュ更新にならない。
