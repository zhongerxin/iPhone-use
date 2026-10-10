# AirDropでMacへファイルを送る

iPhoneの画像、動画、文書などをMacで読み取り・処理・納品する必要がある場合は、PUAで対象アプリの共有・書き出しメニューを操作し、AirDropで現在のMacへ送る。

1. iPhoneで必要なファイルや写真を選び、共有 → AirDrop → 現在のMacを選ぶ。Macの名前が不明なら`scutil --get ComputerName`で確認する。Macに受信確認が表示されたら受け入れる。
2. 送信後は`~/Downloads`で新しいファイルやフォルダーを確認し、絶対パスを使って読み取り・処理・納品する。
