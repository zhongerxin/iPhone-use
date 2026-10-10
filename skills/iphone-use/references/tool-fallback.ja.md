# MCPの不具合とコードからの呼び出し

まず提供されたツールを使い、通常操作は次の必要な観察で進捗も確認する。バインド欠落、ホストの転送エラー、確認済みのラッパー問題はPUA自体の不具合とは限らない。同じ許可済み操作をプラグインのコード入口から続けられる。同じ設定、session、操作ロック、エラー処理を保ち、毎回の追加検証を挟まない。

モデル向け17ツールの、このターンで実際に使えるバインドをALL_TOOLSやツール検索で確認する。tools/list、文書、以前のチャットだけでは判断しない。install.shは同じ名前空間の標準MCPも登録する。導入後にチャットを再接続し、それでも必要なツールが使えない場合だけコード入口を使う。

## エラーを区別する

- schema／引数：argument_path、unknown_fields、allowed_fieldsから修正する。action_executed=falseなら未実行。未知引数を黙って無視しない。
- device_busy：共有ロックの操作が終わるまで待つ。別Runtime設定や状態ディレクトリで迂回しない。
- stale_observation：別RuntimeのIDかアプリ・viewportの変化。次に必要な新情報で位置を決める。全画像ハッシュ、数字更新、固定30秒のために追加読取りをしない。IDは任意。
- 画面異常：selector、焦点、対象未解決、移動なし、遮蔽、文脈変化、入力不一致、期待ページ不在は添付画像を先に見る。ない場合だけ1回取る。ラベル変更や同じ再試行をしない。tap_pointや候補は遮蔽なしを証明しない。見える欄を押してから入力し、焦点がなければ位置を選び直す。
- input_continuation_expired：今回の入力はない。実際の文字を読み、replace=falseで不足だけ補う。
- occluded_target／offscreen_target：対象タップは未実行。画像で見えるならtap_point、覆われていれば先に遮蔽処理、画面外ならスワイプ。独自パネルはAlert／Sheetがない場合がある。
- postcondition_failed：操作は受理されたが結果条件が未達。現在の状態から残りを判断し、自動再実行しない。
- action_executed=true、action_complete=false、uncertain、操作後timeout：部分的に有効かもしれない。入力、送信、削除は実際の欄・記録を読み、コード入口に変えて盲目的に繰り返さない。
- 純照会の一時失敗：上限付きで1回再読できる。POSTの照会を変更操作と誤認しない。
- local.pid.0、pua_foreground_unavailable、XCTest Code 41：READYで接続復旧。selector変更やボタン連打では対処しない。ロック・署名・信頼はsetup、アプリ認証は引き継ぎガイド。

functions.execではtext(block.text)とimage(block)で内容を転送し、base64入り結果をtext(result)にしない。画像転送不能ならview_imageでimage.path／error.observation.image.pathを開く。座標はpixel_to_pointで変換する。

普通のverified=falseはerrorでもbatch停止条件でもない。HTTP受理は呼び出しの境界で、重要な最終状態は別途確認する。Homeは専用homescreen経路を1回使い、必要な場合だけverify=trueでSpringBoardを確認する。HTTP 200でも移動しない現象からiOS／XCTest内部の原因を推測しない。

## READYの復旧

通常はrecover=true、禁止・読み取り専用の場合だけfalse。一時的な前面不良では旧観察・sessionを消して1回再読し、持続時だけ所有者確認済みサービスを再起動する。操作は再実行しない。

recoveringは同じrecovery.job_idをjobs配列から追い、stopping／starting／servingを確認してREADY。Runnerのsucceededを待たずstartを重ねない。段階、ログ、retry_after_secondsに従い、固定回数ループや解析失敗の握りつぶしをしない。recovery_requiredは指示が許す場合だけnext_tool／next_argumentsでtrue。errorなしでも未READY。

設定、endpoint、worker、待受の所有者と有効なビルドが必要。外部・不明サービスや競合は返された手順で処理し、ポートでプロセスを止めない。120秒冷却中はretry_after_secondsに従う。信頼・解除・UIオートメーションの確認は本人が行う。

## 同梱スクリプトを呼ぶ

現在のソースか導入済みプラグインからPLUGIN_ROOTを特定し、他人のキャッシュパスを写さない。

```sh
python3 <PLUGIN_ROOT>/scripts/phone.py pua_press_button '{"name":"home","observe":"none","verify":false}'
```

既定の~/.local/share/iphone-useの共有sessionと操作ロックを使い、JSONを返す。必要なobserve／verifyを同じ呼び出しで選ぶ。画像はimage.pathの縮小JPEGで、同名pngは元画像。画像ツールで開き、pixel_to_pointでポイントへ変換する。元の接続が非既定の場合だけ一致する--state-dir／--urlを使い、busy回避や同じ端末の並列制御に使わない。

## 最小限のコード入口

スクリプトを呼べない場合は同じRuntimeを使う。

```python
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).expanduser().resolve()
sys.path.insert(0, str(root / "server"))
from iphone_use import PUAError, Runtime

runtime = Runtime()
try:
    result = runtime.call("pua_press_button", {"name": "home", "observe": "none", "verify": False})
    print(json.dumps(result, ensure_ascii=False))
except PUAError as error:
    print(json.dumps({"error": error.as_dict()}, ensure_ascii=False))
    raise SystemExit(1)
finally:
    runtime.close()
```

両入口は同じ実装を通る。observation IDはプロセス間共有されないので、使うなら同じRuntimeで取得する。既知の座標・regionでは省略できる。iPhoneポイントを使い、Mac座標や画像ピクセルは渡さない。共有実装の不具合は入口変更だけでは直らず、外部sessionを別途作ったり変更操作を盲目的に繰り返さない。

## スクロールとbatch

既定のswipeはverify=false、observe=noneで1回のみ。既知のregionにツリー・アンカー・IDを強制しない。必要なリスト観察は同じ操作で返す。未検証は未移動という意味ではない。

verify=trueの場合だけ幾何変化、ネイティブパネル、文脈を1回確認する。数値やカルーセルの更新だけを進捗としない。独自パネルは画像で領域・遮蔽を判断する。最初の移動未確認で画像を返して止まり、max_attempts=2でも再試行しない。scroll_findも最大1回のスワイプで、曖昧さ・遮蔽は事前に止まる。画像を見ずにmax_swipesを増やさない。

no_scroll_progressは空リスト・末尾の証拠ではない。scroll_context_changedは操作後の文脈変化。画像失敗は元のエラー・実行証拠を残し、別途1回取得する。batchは通常の未検証操作を続け、error、uncertain、結果条件のないsubmitなどで止まる。completed_steps／stopped_atから続ける。launchは既定1回だけ有効化し、明示verify／expectだけで結果を確認する。

コード呼び出し、Home、1つのアプリ、1つのbatchの完了は途中段階。残りを続け、重要な最終結果と成果物を確認してから終える。
