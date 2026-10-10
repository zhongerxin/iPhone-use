# 障害のある層に応じて復旧する

| 症状・ログ | 最初の確認 | 対応・確認 |
| --- | --- | --- |
| 実機が見つからない、device unavailable | データ対応ケーブル、解除・信頼、Xcodeのペアリング | 再接続してdiscover。対象の実機であることを確認する。 |
| Xcode初回設定、ライセンス、コンポーネント不足 | doctorのdeveloper directory、バージョン、初回状態 | 本人が完全版Xcodeと提示を完了してdoctor。Command Line Toolsだけでは不足。 |
| iOS／developer disk image非互換 | iOS、選択中Xcode、端末準備エラー | 対応するXcodeを選択・更新して準備。同じバイナリーを無限再試行しない。 |
| Developer Mode disabled | 端末の設定と再起動後の確認 | 本人が有効化・再起動しdiscover、start。再起動前の認識は現在の証拠ではない。 |
| signing requires development team／no signing identity | XcodeのApple Account、Team、証明書 | 本人のログイン・Team選択後にconfigure／build。パスワードを設定に保存しない。 |
| bundle identifier cannot be registered／no profiles | Runnerのbundle IDとチームのprovisioning | 署名可能なIDでconfigureし再ビルド。XCTest Runnerの.xctrunner接尾辞も考慮。 |
| maximum number of apps for free development profiles | Personal Teamの開発用アプリ数 | 実際の制限を説明し、不要アプリの削除や既存の別Teamは本人が選ぶ。自動削除や購入の強制はしない。 |
| profile／certificate expired、旧Runnerが起動しない | 署名ログとprofile期限 | 既存設定でbuild／startしREADY。無料profileの期限は実際のprofileを確認。ポートを開き直すだけでは直らない。 |
| developer not trusted／unable to verify | 端末の開発者項目、ネットワーク、Xcode表示 | 本人が信頼・検証。共有証明書を失効させない。 |
| BUILD SUCCEEDEDだがHTTP到達不可 | Runnerテスト、USB転送、Mac側ポート | statusで各層を調べ、必要なstart。ビルド成功はREADYではない。 |
| ポート競合 | 正しい端末の転送か、無関係なサービスか | 正しい転送を再利用するか空きポートを設定。所有者不明のプロセスを止めない。 |
| status到達可能だがsession／source失敗 | 対応端末、session、ロック、テスト | エラー・状態を読み、必要なsession再作成・Runner再起動後にviewportと観察を確認。 |
| stale element referenceにlocal.pid.0／pua_foreground_unavailable | 実際の前面を解決できるか | READYでsession・画面を1回再読。継続時は確認済みの背景復旧を追う。旧観察・要素IDは無効。 |
| XCTest Code 41／Not authorized for performing UI testing actions | テストの認可、端末の開発設定 | READYで復旧。継続なら本人が解除、開発モード、UIオートメーション、信頼を確認。ボタン連打しない。 |
| ready=false、state=recovering | recovery.job_idのstatusとログ | 同じジョブを追い、servingでREADY。isError=falseはREADYではない。succeededを待たずstartを重ねない。 |
| recovery_required、recovery_disabled | recover=false、再起動禁止・読み取り専用指示 | 指示が許せばnext_tool／next_argumentsでtrue。禁止は保つ。通常の初回READYでは既定true。 |
| pua_recovery_required、cooldown／manual | 残り冷却、設定、worker、待受所有者、ビルド | 120秒冷却とretry_after_secondsに従う。外部・不明プロセスをポートで止めない。 |
| 操作後のHTTP timeout | 実際の画面・欄・メッセージ | 入力・送信が有効かもしれないため結果を読んで残りを処理。純照会は上限付き再読。 |
| action_executed=true、action_complete=false | 受理済み操作の現在の状態 | uncertain=falseでも再読し、全文・送信・batchを繰り返さない。接続復旧は業務確認ではない。 |
| アプリのログイン・認証 | 現在の認証表示と正規の入口 | [本人への引き継ぎ](../../iphone-use/references/authentication.ja.md)。完了回答後に新しい観察。アプリ認証だけでPUAを再起動しない。 |
| ツリー欠落・ラベル特定の失敗 | 独自描画、保護、切れ | 結果の画像から座標操作。入力は欄をタップ後selectorなし。観察不能なら具体的な制限を報告。 |

statusはjobs配列を返す。idで選び、存在しない単独jobを参照しない。fetch／buildは終状態、通常startはservice.ready=trueでREADY、recoverはservingでREADYへ進む。現在の段階、ログ、retry_after_secondsを使い、固定の長い待機・重複startをしない。

READYのobservationを次に使う。通常のタップ、Home、起動、入力、スクロールのverified=falseは停止や追加観察の理由ではない。未知ページの情報は同じ操作で返し、重要な最終結果を明示確認する。既知の座標・regionではIDのためだけに読み取らない。IDを使うなら同じRuntimeとアプリ・viewportに限る。

操作はMacのミラーリング窓や画面原点に依存しないが、ロック、本人の操作、USB切断、アプリの認証は実機の状態を変える。現在の証拠から判断し、ミラーリング用のロック手順を当てはめない。

署名資料：[Apple Personal Team](https://developer.apple.com/help/account/basics/about-your-developer-account)、[Appium provisioning](https://appium.github.io/appium-xcuitest-driver/latest/getting-started/provisioning-profile/)、[Appium自動署名](https://appium.github.io/appium-xcuitest-driver/latest/getting-started/provisioning-profile/auto-config/)。上流の確認日：2026-10-06。

## ミラーリングとの競合

mirroring_conflict、設定画面の空ツリー、Macから使用中のロック表示ならMacのiPhoneミラーリングを終了し、必要なら本人が解除してREADY。空ツリーのままlaunch・tap・強制入力を繰り返さない。プロセスの存在だけでは占有の証拠にならず、ツールは空ツリーとミラーリング動作の両方を検出してREADYを拒否する。
