# 実際のアプリのbundle IDを探す

確認済みのIDなら直接起動し、毎回インストール一覧を読み直さない。既知のアプリはpua_apps(source="catalog")でオフライン検索する。未知のアプリや版の区別が必要な場合はsource=autoで選択中iPhoneのインストール一覧とローカル一覧を検索し、候補がなければAppleを検索する。ブランド名からIDを作ったり、推測値を連続試行しない。

```json
{"query":"WeChat","source":"auto","country":"jp"}
```

名前、公開元、候補の出典を確認してbundle_idをpua_launch_appに渡す。未知ページが必要なら起動時のobserveで前面とページも読み、専用確認を重ねない。招商銀行の本体com.cmbchina.MPBBankとクレジットカード用の掌上生活は別アプリ。

- source=auto：選択端末の一覧（300秒キャッシュ）と同梱一覧を統合。正確な名称・別名を優先し、一致があれば部分一致を混ぜない。ローカル候補がなければAppleへ。
- source=installed：選択iPhoneだけを検索。端末未選択や取得失敗は診断を返す。ブランドの英語別名は同梱一覧でIDへ対応させられる。
- source=catalog：apps.jsonの36アプリをオフライン検索する。端末やネットワークを照会しない。同梱一覧は上流が確認した中国・香港のアプリを中心にしており、実際の名前・ID・出典を保持している。
- source=apple：指定したApp Store地域の公開APIを検索する。既定はcountry=cn。日本ならjp、香港hk、米国usを明示する。ストア候補は実機インストールの証拠ではない。

installed_verified=trueは実機のインストール一覧で確認済み。installation_checkedは今回一覧を取得できたか（最大5分キャッシュ）で、falseだけでは未インストールとはいえない。verified_atは出典の確認日時。対象候補だけを返す。端末一覧とキャッシュはリポジトリー外の非公開状態先（600）に保存し、Appleには送信しない。

## Appleの公式API

[Apple iTunes Search APIの資料](https://performance-partners.apple.com/search-api)はsoftware、地域、JSON、キャッシュ、毎分約20件の目安を説明している。公開メタデータの検索にApple ID、パスワード、APIキーは不要。

```text
https://itunes.apple.com/search?term=WeChat&country=jp&media=software&entity=software&limit=10
https://itunes.apple.com/lookup?id=414478124&country=jp&entity=software
```

名称検索で公開元と製品を確認し、安定したtrackIdのLookupで更新する。検索の先頭を自動選択しない。実装はURLエンコード、5秒timeout、応答サイズ上限、Apple HTTPSのドメイン・パス・リダイレクト制限、15分キャッシュ、毎分最大18件の新規要求で制限する。

```sh
python3 scripts/update_app_catalog.py --search "アプリ名" --country jp
python3 scripts/update_app_catalog.py --refresh
```

検索は候補を表示するだけで一覧に自動追加しない。保守者がストア、公開元、製品を確認して追加する。refreshは既存の確認済み記録のメタデータだけを更新し、ID欠落やbundle ID変更なら停止して元を保持する。

## 範囲と失敗

同名、地域、配信終了で検索できない場合がある。企業内、開発版、未公開アプリはストアに存在しないことがある。[App Store ConnectのbundleIds](https://developer.apple.com/documentation/appstoreconnectapi/get-v1-bundleids)は自チームの登録IDだけで、全第三者アプリの一覧ではない。実機一覧が本人のアプリの直接的な証拠になる。

上流の確認では富途牛牛はCNで見つからずHKでcn.futu.FutuTraderPhoneを取得した。空ならsource=installedまたは実際の地域を調べ、検索不可を未導入とみなさない。飛書とLark、WeChatと企業WeChat、通常版と軽量版・開発版は区別する。

MCP照会が使えない場合は、Xcodeで一覧を読みローカルで対象だけを抽出する。--include-all-appsを保つ（devicectlの既定は開発アプリのみ）。ファイルはリポジトリー外の非公開先に置き、完全な端末一覧をプロジェクトへ保存しない。

```sh
xcrun devicectl device info apps --device "<選択端末のUDID>" --include-all-apps --json-output "<非公開ディレクトリ>/apps.json" --timeout 8
```

## 上流で確認した一覧

実際のAPI URL、ストア名、公開元、地域、UTC確認日時は[apps.json](apps.json)。下表は元のブランド表記、bundle ID、確認地域を保持する。ストア記録は端末への導入を証明しない。

| アプリ | bundle ID | 確認したストア |
| --- | --- | --- |
| 微信 | `com.tencent.xin` | [CN](https://apps.apple.com/cn/app/id414478124) |
| 支付宝 | `com.alipay.iphoneclient` | [CN](https://apps.apple.com/cn/app/id333206289) |
| 淘宝 | `com.taobao.taobao4iphone` | [CN](https://apps.apple.com/cn/app/id387682726) |
| 京东 | `com.360buy.jdmobile` | [CN](https://apps.apple.com/cn/app/id414245413) |
| 拼多多 | `com.xunmeng.pinduoduo` | [CN](https://apps.apple.com/cn/app/id1044283059) |
| 小红书 | `com.xingin.discover` | [CN](https://apps.apple.com/cn/app/id741292507) |
| 抖音 | `com.ss.iphone.ugc.Aweme` | [CN](https://apps.apple.com/cn/app/id1142110895) |
| 快手 | `com.jiangjia.gif` | [CN](https://apps.apple.com/cn/app/id440948110) |
| 哔哩哔哩 | `tv.danmaku.bilianime` | [CN](https://apps.apple.com/cn/app/id736536022) |
| 微博 | `com.sina.weibo` | [CN](https://apps.apple.com/cn/app/id350962117) |
| QQ | `com.tencent.mqq` | [CN](https://apps.apple.com/cn/app/id444934666) |
| QQ音乐 | `com.tencent.QQMusic` | [CN](https://apps.apple.com/cn/app/id414603431) |
| 网易云音乐 | `com.netease.cloudmusic` | [CN](https://apps.apple.com/cn/app/id590338362) |
| 百度地图 | `com.baidu.map` | [CN](https://apps.apple.com/cn/app/id452186370) |
| 高德地图 | `com.autonavi.amap` | [CN](https://apps.apple.com/cn/app/id461703208) |
| 滴滴出行 | `com.xiaojukeji.didi` | [CN](https://apps.apple.com/cn/app/id554499054) |
| 美团 | `com.meituan.imeituan` | [CN](https://apps.apple.com/cn/app/id423084029) |
| 饿了么 | `me.ele.ios.eleme` | [CN](https://apps.apple.com/cn/app/id507161324) |
| 携程旅行 | `ctrip.com` | [CN](https://apps.apple.com/cn/app/id379395415) |
| 飞猪 | `com.taobao.travel` | [CN](https://apps.apple.com/cn/app/id453691481) |
| 铁路12306 | `cn.12306.rails12306` | [CN](https://apps.apple.com/cn/app/id564818797) |
| 百度网盘 | `com.baidu.netdisk` | [CN](https://apps.apple.com/cn/app/id547166701) |
| 腾讯会议 | `com.tencent.meeting` | [CN](https://apps.apple.com/cn/app/id1484048379) |
| 钉钉 | `com.laiwang.DingTalk` | [CN](https://apps.apple.com/cn/app/id930368978) |
| 飞书 | `com.bytedance.ee.lark` | [CN](https://apps.apple.com/cn/app/id1401729613) |
| 企业微信 | `com.tencent.ww` | [CN](https://apps.apple.com/cn/app/id1087897068) |
| 招商银行 | `com.cmbchina.MPBBank` | [CN](https://apps.apple.com/cn/app/id392899425) |
| 掌上生活 | `com.cmbchina.cmblife` | [CN](https://apps.apple.com/cn/app/id398453262) |
| 中国工商银行 | `com.icbc.iphoneclient` | [CN](https://apps.apple.com/cn/app/id423514795) |
| 中国建设银行 | `com.ccb.ccbDemo` | [CN](https://apps.apple.com/cn/app/id391965015) |
| 中国农业银行 | `com.bankabc.iphonerelease` | [CN](https://apps.apple.com/cn/app/id515651240) |
| 中国银行 | `com.boc.BOCMBCI` | [CN](https://apps.apple.com/cn/app/id399608199) |
| 雪球 | `com.xueqiu` | [CN](https://apps.apple.com/cn/app/id492180369) |
| 同花顺 | `cn.com.10jqka.IHexin` | [CN](https://apps.apple.com/cn/app/id303191318) |
| 东方财富 | `com.eastmoney.iphone` | [CN](https://apps.apple.com/cn/app/id423525686) |
| 富途牛牛 | `cn.futu.FutuTraderPhone` | [HK](https://apps.apple.com/hk/app/id592031984) |
