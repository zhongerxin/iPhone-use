---
name: iphone-use
description: PUA（Phone Use Agent）のMCPツールで実機のiPhoneを操作する。新しいチャットでは先にsetupでサービスを確認・起動してからREADYを取得する。移動、タップ、入力、スクロールを実行し、次の判断に必要な観察で進捗も確認する。重要な最終結果を検証し、アプリ検索、リスト収集、画面サイドバー、パスワードやFace IDのユーザー引き継ぎを扱う。ユーザーには日本語で案内する。
---

# PUAでiPhone上の作業を実行する

このチャットで有効なREADYの証拠がなければ、以下の初期化を行ってからiPhone上の作業を始める。以前のチャットでの成功、プラグインの導入、ウィジェットやRunnerのアイコンはREADYの代わりにならない。サービス未起動は続けて処理する導入手順であり、作業全体を終了する理由にしない。既存の設定を再利用し、足りないサービスだけ起動する。

ユーザーが必要とする成果と完了条件を定める。通常の正常応答は予定した操作が実行されたものとして次へ進む。タップ、画面移動、Home、起動、入力のたびに専用の確認を挟まない。次の判断に画面情報が必要なら一度取得し、直前の操作の効果もそこで判断する。明確に失敗した場合に修正する。最終的な重要操作、送信・提出結果、重要な文章、成果物には実際の結果の証拠が必要。

ツールの`ok`、`verified`、`complete`やホストのcompletedは個々の呼び出しに関する状態であり、作業全体の完了を示さない。`action_complete`はコマンドの処理完了を示す。正常応答の`verified=false`は個別検証を省略しただけで、失敗ではない。完了条件を満たし、重要な結果を確認して成果物を渡すまで進める。「次は読み取ります」で終えない。長い作業では短い進捗を伝える。

## READYと認証

新しいチャットで最初にiPhoneを使うときは、先に`pua_setup(action="status")`を呼び、[起動と復旧](references/startup.ja.md)に従って正常なサービスか活動中のジョブを再利用する。必要なサービスがなければstartを一度だけ呼ぶ。サービスが使える状態になってから`pua_ready(recover=true, screenshot=false)`を呼ぶ。READYの失敗を待ってからsetupを始めない。文字の作業ではstatus、session、tree、viewport、ロック解除の確認を残し、不要な画像は取らない。このチャットですでにREADYで接続が正常なら再利用する。`recover=false`はユーザーが再起動を明示的に禁止した場合や読み取り専用の診断を求めた場合に限り、制限を守る。

- `ready=true, state="ready"`：その`observation`を次の判断に使う。直後にobserve、doctor、移動テストを重複実行しない。
- `ready=false, state="recovering"`または`state="recovery_required"`：errorがなくMCPのisErrorがfalseでも、操作可能という意味ではない。[起動と復旧](references/startup.ja.md)に従い同じジョブを調べる。
- `pua_unreachable`、接続拒否、`not_ready`などの未起動：`recover=true`は停止中サービスのコールドスタートを行わない。`pua_setup(action="status")`で活動中のstart／recoverを再利用するか、設定済みで該当ジョブがなければstartを一度呼び、READYを再確認する。設定・ソース・ビルドが不足するときは`iphone-use-setup`を読む。

読み取り専用、起動・再起動禁止などの指示を守る。復旧後の新しいREADY観察から元の作業の進捗を確認し、すでに有効になった可能性がある操作を再実行しない。

READYは画面サイドバーに関連付けられ、ホストが対応する場合は同じチャットのパネルを開くか再利用する。setupの再実行、接続復旧、プレビューの一時停止・再開でも同じウィジェットを使う。開いているなら画面を開くツールを重ねない。閉じたパネルを開くときは`pua_screen()`を一度呼び、画面のためにREADYを繰り返さない。ユーザーが「先に画面を見せて」と言ったら開いてから初期化と許可済み作業を続け、明示的に確認待ちを求めた場合のみ待つ。

上部は機種とLive状態、下部は更新・ホーム・スクリーンショットのボタン。ボタンはユーザー向けで、モデルはApp専用の`pua_screen_frame`と`pua_screen_action`を呼ばない。プレビュー更新のための画像・observe・ポーリングを増やさない。ユーザーがHomeを押した後は次の実際の観察に従う。空白、光、カーソル、プレビューはREADYや操作成功の証拠でもモデルの観察でもない。[画面ガイド](references/screen.ja.md)に従い、表示の問題だけで操作接続を繰り返し復旧しない。

パスワード、PIN、認証コード、Face ID／Touch ID、ロック解除が実際に必要な場合は[認証の引き継ぎ](references/authentication.ja.md)に従う。アプリ認証は先に`pua_screen(action="pause")`。`phone_locked`は`device_locked`で自動停止するため明示pauseで理由を上書きしない。ホストの質問ツール（Defaultでは`functions.request_user_input_async`を優先）を必ず使い、先頭の選択肢を「完了したので続けてください」、次を「今は完了できません」とする。非同期の戻り値や初期選択は回答ではない。引き継ぎ中は端末操作、読み取り、画像取得を止め、認証情報を求めない。

実際の完了回答後、アプリ認証または旧版のunknown停止は`pua_screen(action="resume")`から新しく観察する。ロック解除はREADYを再確認し、成功時に同じロック停止だけが解除される。READYの`preview.paused`と`pause_reason`を読み、READY成功をアプリ認証の完了とみなさない。現在の状態から残りを続ける。

## 次の判断に必要な観察を選ぶ

一意な対象と既知の経路では`observe="none"`を使い、連続実行またはbatchにまとめる。未知のページ、領域、内容を次に判断するなら、その操作で`observe="tree"`、`"screenshot"`、`"both"`を指定して返された`observation`を使う。noneの後で専用の確認observeを挟まない。独立した読み取りは`pua_observe(mode=...)`を使い、modeとobserveを混同しない。

要素の`type`は`XCUIElementType`を省略し、`rect`は`[x, y, width, height]`（iPhoneのポイント）。name省略はlabelと同じ、value省略は文字と同じ、enabled／visible／in_viewport省略はtrueを意味する。通常は`include_invisible=false`、`max_nodes=200`、`expensive_visibility=false`。単一対象の確認は`pua_find`／`pua_wait`を使う。ツリーが切れていることや視野内の要素は、全件取得や遮蔽なしを証明しない。固定ヘッダーや浮いたパネルが覆う場合がある。

独自描画、遮蔽、ラベル欠落など画像の判断が必要なときにスクリーンショットを取る。同じ結果の画像は読みやすいサイズに縮小される。画像ピクセルに`image.pixel_to_point`の`[x, y]`を掛けてiPhoneポイントに変換し、元の解像度やMacの画面座標を使わない。screenshotはXMLを省略し、bothは両方を返す。画像取得で共有パネルが出たら現在の状態を処理し、同じ操作を繰り返さない。

## 画面の異常時は先に画像を見る

selectorやフォーカスの失敗、スクロール後も対象を操作できない、移動がない、遮蔽や文脈の変化、入力不一致、期待ページがない場合は結果の現在の画像を見る。画像がない場合だけ`pua_observe(mode="screenshot")`を一度呼ぶ。先にツリーを読み直す、ラベル表記を変える、スクロール回数を増やす、同じ操作を再試行することは避ける。ツリーの切れ、座標タップの明確な失敗、想定外のページも同様。正常な`verified=false`に毎回画像は不要。

現在のページ、先頭・末尾行の移動、末尾の要素や余白、ポップアップ・固定ヘッダー、実際のスクロール領域を確認する。対象または取得範囲の境界が見えたら検索を止める。移動が止まったら境界・遮蔽を処理してから領域や方向を判断する。`direction`は指を動かす向きで、upは通常、下に続く内容を表示する。ユーザーのライブプレビューはツール画像の代わりにならない。

- タップ：`tap_point`やcandidatesの`tap`は要素の位置で、タップ可能とは限らない。クーポン、広告、メニュー、ログインパネルなどの実際に見える閉じる／キャンセルを画像で探してから対象を押す。
- 入力：画像で見える入力欄をタップし、selectorなしの`pua_type_text(text=...)`を使う。`no_focused_field`は未入力を示す。同じ座標と入力をそのまま繰り返さず、画像から選び直す。
- 画面外：対象へスワイプしてから、その後の画像に従ってタップする。待機や検索の失敗後も同じラベルのループを続けない。

画像の`(px, py)`を`x=px*image.pixel_to_point[0]`、`y=py*image.pixel_to_point[1]`に変換して`pua_tap`に渡す。x／yはiPhoneポイントで、画像ピクセルやMacウィジェット座標ではない。

`functions.exec`でツールを呼ぶときは画像を実際にモデルへ転送し、base64入りの全結果を`text(result)`で出力しない。

```javascript
const result = await tools.mcp__iphone_use__pua_observe({mode: "screenshot"});
for (const block of result.content ?? []) {
  if (block.type === "text") text(block.text);
  else if (block.type === "image") image(block);
}
```

タップや入力の失敗結果も画像ブロックを転送する。転送できなければ`view_image`で`image.path`／`error.observation.image.path`を開く。メタデータやbase64を見たことを画像の確認とみなさない。

パラメーターの誤りはschemaに従って修正し、接続障害はREADYで復旧、ロック・認証は本人の操作を待つ。結果が不確実なら現在の状態を読んでから続け、入力、送信、注文、batch全体を再実行しない。画像で解決できる通常の遮蔽は引き続き処理する。

## アプリと通常の操作

iPhoneの画像、動画、文書などをMacで読み取り・処理・納品する必要がある場合は、[AirDropでMacへファイルを送る](references/airdrop.ja.md)を参照する。送信後はダウンロードフォルダーで受け取ったファイルを確認する。

確認済みのbundle IDなら直接起動する。オフラインのアプリ検索は`pua_apps(source="catalog", query=...)`または[アプリ一覧](references/apps.ja.md)。未知・同名のアプリは`source="auto"`で実機候補を探し、それでもなければAppleを検索する。IDを連続して推測しない。招商銀行の本体は`com.cmbchina.MPBBank`。ストア情報はインストールの証拠ではなく、`installed_verified=true`を確認する。起動後の必要な観察で実際の前面も読み、追加の起動確認を既定で挟まない。

- `pua_launch_app`：一度有効化して次へ進む。既定は`verify=false, observe="none"`。重要な入口の確認が必要なら`verify=true`または`expect`。
- `pua_tap`：現在の要素のlabel／name／value／typeを正確に写したselectorを優先し、enabledを加えられる。長い・変化するラベルは`label_contains`。predicateは単独で使い、実際の改行・引用符・バックスラッシュを保持する。rect、visible、in_viewportはselectorではない。同じ場所の重複や画面内に1件だけある場合はツールが解決する。別々の複数候補は`ambiguous_target`とindex、種類、位置、tap、hittableを返す。そのtap座標または同じselectorとindexを使い、先頭を無条件に選ばない。情報が足りれば余分な観察は不要。任意のobservation_idを渡すなら同じRuntime由来で、アプリ・視野の文脈を確認する。ページ移動、ユーザー引き継ぎ、回転後は現在の情報を使う。
- `pua_press_button`：schemaのボタンだけを使う。Homeは専用homescreen経路で既定`verify=false`。必要な場合に`verify=true`。MCPのバインドが使えなければ[コードでの呼び出し](references/tool-fallback.ja.md)を使う。
- `pua_swipe`：指の移動方向を指定し、既定`verify=false, observe="none"`で1回だけ実行する。既知のregionならツリーやIDは必須ではない。次のリスト読み取りで移動も判断し、動かなければ画像で境界や遮蔽を調べる。`verify=true`は幾何変化を1回確認し、失敗なら画像で止まる。旧`max_attempts=2`でも自動再試行しない。
- `pua_wait`：対象の出現が実際に必要なときだけ上限付きtimeoutで待つ。固定の長いsleepや全移動後のwaitは不要。
- `pua_scroll_find`：すでに操作可能なら即座に返す。遮蔽・曖昧さは画像付きで停止。1回の呼び出しで最大1回だけスワイプし、未解決なら画像を見て次を判断する。`max_swipes=0`は検索のみ、正数が1より大きくても連続で盲目的に動かさない。見つかった結果を再検索しない。

`expect`と`verify=true`は重要な最終状態や実際の依存条件のための選択肢。HTTPの受理と業務上の成功を区別する。明確なerror、入力・送信のuncertain、部分実行は現在の状態から残りを判断する。

## 入力と送信

`pua_type_text(selector, text)`に必要な文章を一度に渡す。短いテスト文章やASCIIを先に入れず、自分で分割しない。selectorの失敗後は座標で入力欄をタップし、selectorなしで入力する。既定は`replace=true, allow_newlines=false, submit=false, verify=false, observe="none"`。通常の検索・絞り込みは次へ進み、未知のページなら同じ呼び出しで観察を返す。下書きを保持する場合は現在の内容に応じて置換か追記を選ぶ。重要な最終文章には`verify=true`を使える。パスワード、ロック解除コード、認証コードは本人が入力する。

長文はツールが分割する。`input_complete=false`なら返された`continue_token`だけで再度呼び、文章や他の引数を渡さない。入力完了までタップ、スワイプ、移動をしない。元のverify／submit／expect／observeは全入力後に実行される。`input_continuation_expired`では何も入力されないため、欄の実際の文字を読んで`replace=false`で不足部分だけを補う。途中のエラーの`characters_confirmed`も読み戻し、全文を再入力しない。

チャット欄はReturnで送信する場合がある。適切な複数行TextViewと確認できた場合だけ`allow_newlines=true`。指定された書式を黙って1行に変えない。改行の許可は送信の許可ではない。送信が許可されている場合は宛先と完全な下書きを確認して1回送信し、最終内容と送信回数を確認する。`submit=true`だけでは成功を証明しない。未送信が明確な場合だけ補い、timeoutや普通の`verified=false`を理由に再送しない。

WeChatでは可能なら改行なしの1通にまとめる。複数行が必要なら`\n`を含む全文をそのまま入力しない。下書きを入れてフォーカスを確認し、入力欄内の余白から編集toolbarを開いて、実際に表示された改行項目（中国語UIでは「换行」）を押す。必要に応じて繰り返し、全文を確認して送信項目（「发送」）を1回押す。toolbarや改行項目がなければ現在の画面から判断し直し、Returnで代用しない。UIの言語・バージョンに従い、`allow_newlines=true`だけを根拠に安全な改行を推測しない。

## 連続操作とリスト収集

既知の短い経路は`pua_batch`に最大20ステップまとめる。通常操作にexpectは不要で、普通の`verified=false`は続きを止めない。必要な位置または最後に観察を置く。明確なエラー、不確実な操作、結果条件のないsubmit、長文未完了（`stop_reason="input_continues"`）、時間上限（`"time_budget"`）では停止する。completed_steps／stopped_atと各結果から残りを続け、全batchを再実行しない。未知画面、認証、動的ポップアップでは新情報に応じて区切り、許可されていない送信を混ぜない。

位置が未知の記録を探す際、同方向のスワイプを複数batchに詰めない。1回動かして必要なページを読み、ツリーの切れや移動の停止は画像で判断する。取得範囲や末尾が見えたら止める。

`pua_collect_list(row_type="Cell", max_pages=6)`は最大10ページを上限付きで取得する。各回1回だけスワイプし、新ページを収集する。完全なツリーとviewportを再利用し、対象行から重複ページを判断する。仮想化で全ラベルが変わっただけでは新ページを捨てず、予備のジェスチャーも自動試行しない。rows、pages、stop_reasonを返し、completeは常にfalse。同じtype／name／label／valueは重複除去され、同じ表示の別記録が統合される場合がある。end_selectorを範囲の証拠に使え、最終的に範囲、件数、合計、日付、欠落項目を確認する。上限・移動停止は全件取得の証拠ではない。詳細不足は詳細画面で読む。[重要な結果の確認](references/verification.ja.md)を参照する。

## 明確な異常への対応

`occluded_target`はクリック前の失敗でタップは未実行。画像で対象が見えればtap_pointで押し、実際のパネルや固定ヘッダーが覆うなら先に処理する。安全に閉じられる実際の入口を選ぶ。独自描画パネルにはAlert／Sheetがない場合がある。`offscreen_target`は対象を画面に入れる。

`no_scroll_progress`はジェスチャーを実行したが移動を証明できない状態で、空リストや末尾の証拠ではない。`scroll_context_changed`、領域の遮蔽、リストの重複ページ停止も画像を返す。none／tree指定でも異常時の画像を使い、取得済み行と範囲制限を残す。回数を盲目的に増やさない。

`action_executed=true, action_complete=false`または`uncertain=true`では部分的に有効な可能性がある。入力、送信、支払い、注文は実際の内容・記録を読み、再実行しない。純粋な照会の一時的失敗はツールで上限付き再読できる。継続する`local.pid.0`、`pua_foreground_unavailable`、XCTest Code 41はREADYで復旧し、selectorを変えない。[コードでの呼び出し](references/tool-fallback.ja.md)でも同じ実装・状態・操作ロックを保持する。

`pua_metrics`はHTTPとツールの所要時間、各ツールの応答バイト数、応答から次の呼び出しまでの待ち時間（ホスト・モデル・ユーザーの時間の合計）を返す。改善は実際の作業時間と成果物の完成度で判断し、各操作の個別検証を省略したことを業務上の成功の証明とはしない。

完了時は重要な結果と未確認範囲を明示し、ユーザーに必要な成果物を渡す。個々のツール成功を作業全体の完了とみなさない。
