ntx-diskimage-manager
=====================

ntx format diskimage manager

ntx(Netronix)形式diskimageの抜出・挿入(入れ替え)ツールです。

* S/N領域
* HW_CONFIG領域
* u-boot領域
* kernel領域
* initrd領域
* initrd2領域
* waveform領域
* logo領域

iniファイルでファイルや操作領域を指定して実行すると
設定にしたがって抜出等の作業を行います。

Python 3.3.2 @ Windows7(64bit)でしか確認していません。
(問題が生じるとしたらmmap関連とencodingがらみか？)

32bit環境で4GBクラスのイメージを使おうとすると実行できないと思います。
(イメージ全域を無条件にmmapしてるので…)

ddのテンプレ出力するモードもありますが、ntxbinヘッダの扱いが面倒なので
raw操作しか意味がないと思います。
hexdump等でheaderのsizeを取り出してshellベースでゴニョって
data本体の読み出しサイズとしてshell変数設定するようなことをすれば
テンプレモードでもntxbin対応できるはずですが、
そこまでしてテンプレスクリプトを生成する意味もないので…

####
sage: ntx-diskimage-manager.py [-h] [-f DISKMAP] [-d DD] [-t] [--debug]
                               [--encoding ENCODING]
                               config-file

positional arguments:
  config-file

optional arguments:
  -h, --help            show this help message and exit
  -f DISKMAP, --diskmap DISKMAP
                        diskmap spec file
  -d DD, --dd DD        dd program filepath
  -t, --dd-template     outut dd template
  --debug               enable debug mode
  --encoding ENCODING   encoding for config file

通常の使用の場合は
	ntx-diskimage-manager.py sample.ini

iniファイルに日本語コメント等を書く場合でencodingがplatform nativeと異なる場合は
"--encoding ENCODING"で指定する

特殊な形式等diskmap自体をいじる場合は
"-f DISKMAP"でjson形式のdiskmap定義ファイルを指定する

ddテンプレート出力機能などはデバッグ用途で適当に実装しているだけなので
使う必要はありません。

####
diskmap.json
	ntx形式の領域定義ファイル(JSON形式)
	*** JSON形式なのでencodingはUTF-8です ***
*.ini
	操作指示ファイル
	*** platform native以外のencodingの場合は利用時にオプション指定が必要 ***

